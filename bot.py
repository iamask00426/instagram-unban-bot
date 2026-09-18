import os
import asyncio
import aiohttp
import logging
from datetime import datetime
from PIL import Image, ImageDraw
from io import BytesIO
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8663562942:AAGLFTtUT2V-uH0t3eWHwwcVfxVBEXuqZJg")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "apify_api_Quo24KgwXfYdIQUBBoDMxtzzUUzqk20IcBPG")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Monitored accounts dictionary
# Key: username.lower(), Value: {"username": raw_username, "chat_id": int, "start_time": datetime, "mode": "unban"|"ban"}
monitored_accounts = {}


def format_num(count):
    try:
        num = float(count)
        if num >= 1_000_000:
            val = num / 1_000_000
            return f"{val:.1f}m" if val % 1 != 0 else f"{int(val)}m"
        elif num >= 1_000:
            val = num / 1_000
            return f"{val:.1f}k" if val % 1 != 0 else f"{int(val)}k"
        return str(int(num))
    except Exception:
        return str(count)


async def create_profile_card(username, followers, posts, following, pic_url):
    """Creates an Instagram profile preview card image using PIL"""
    img = Image.new('RGB', (600, 200), color='#1c1b22')
    draw = ImageDraw.Draw(img)
    
    if pic_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(pic_url, timeout=5) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
                        avatar = Image.open(BytesIO(avatar_bytes)).convert("RGB").resize((100, 100))
                        mask = Image.new('L', (100, 100), 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, 100, 100), fill=255)
                        img.paste(avatar, (50, 50), mask)
        except Exception as e:
            logging.error(f"Avatar download error: {e}")

    draw.text((170, 50), f"@{username}", fill="white")
    draw.text((170, 105), f"{posts} posts    {followers} followers    {following} following", fill="#b0b0b0")
    
    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio


async def check_batch_accounts(usernames_list):
    """Batch fetch profile status using Apify Instagram Profile Scraper"""
    API_URL = f"https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    found_accounts = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, json={"usernames": usernames_list}, timeout=30) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    for item in data:
                        uname = item.get("username") or item.get("ownerUsername")
                        if uname:
                            url_val = str(item.get("url", ""))
                            followers = item.get("followersCount", 0)
                            
                            if "error" not in url_val and followers is not None and followers > 0:
                                found_accounts[uname.lower()] = {
                                    "username": uname,
                                    "followers": format_num(followers),
                                    "posts": format_num(item.get("postsCount", 0)),
                                    "following": format_num(item.get("followsCount") or item.get("followingCount", 0)),
                                    "pic_url": item.get("profilePicUrl", "")
                                }
    except Exception as e:
        logging.error(f"Batch Error: {e}")
    return found_accounts


async def monitor_loop():
    """Background monitoring loop for Unban and Ban alerts"""
    while True:
        if monitored_accounts:
            usernames = list(monitored_accounts.keys())
            for i in range(0, len(usernames), 30):  # Process in batches of 30
                batch = usernames[i:i+30]
                live_accounts = await check_batch_accounts(batch)
                
                for uname in batch:
                    if uname not in monitored_accounts:
                        continue

                    data = monitored_accounts[uname]
                    raw_username = data.get("username", uname)
                    chat_id = data["chat_id"]
                    mode = data.get("mode", "unban")

                    t = datetime.now() - data['start_time']
                    h, r = divmod(int(t.total_seconds()), 3600)
                    m, s = divmod(r, 60)
                    elapsed_str = f"{h}h {m}m {s}s"

                    # UNBAN MODE: Trigger alert when account becomes LIVE
                    if mode == "unban" and uname in live_accounts:
                        ig = live_accounts[uname]
                        msg = (
                            f"✅ <b>Username unbanned!</b>\n\n"
                            f"<a href='https://instagram.com/{raw_username}'>@{raw_username}</a> is now active again — <a href='https://instagram.com/{raw_username}'>View Profile</a>\n"
                            f"Followers: {ig['followers']}\n"
                            f"Time elapsed: {elapsed_str}"
                        )
                        try:
                            card_img = await create_profile_card(raw_username, ig['followers'], ig['posts'], ig['following'], ig['pic_url'])
                            await bot.send_photo(chat_id, photo=types.BufferedInputFile(card_img.read(), filename="card.png"), caption=msg)
                        except Exception as e:
                            logging.error(f"Error sending unban photo: {e}")
                            await bot.send_message(chat_id, text=msg, disable_web_page_preview=False)
                        
                        del monitored_accounts[uname]

                    # BAN MODE: Trigger alert when account is NO LONGER live
                    elif mode == "ban" and uname not in live_accounts:
                        msg = (
                            f"🚨 <b>Super-Fast Ban Alert!</b>\n\n"
                            f"<a href='https://instagram.com/{raw_username}'>@{raw_username}</a> has been <b>BANNED/DISABLED</b>!\n"
                            f"Time elapsed: {elapsed_str}\n\n"
                            f"🔗 <a href='https://instagram.com/{raw_username}'>View Profile</a>"
                        )
                        try:
                            await bot.send_message(chat_id, text=msg, disable_web_page_preview=False)
                        except Exception as e:
                            logging.error(f"Error sending ban alert: {e}")
                        
                        del monitored_accounts[uname]

        # Polling interval
        await asyncio.sleep(15)


@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: types.Message):
    welcome_text = (
        "🤖 <b>Truzd Instagram Monitor Bot</b>\n\n"
        "Commands:\n"
        "• <code>/monitor &lt;username&gt;</code> — Track single IG account for UNBAN.\n"
        "• <code>/banmonitor &lt;username&gt;</code> — Track active IG account for BAN.\n"
        "• <code>/bulk &lt;user1&gt; &lt;user2&gt; ...</code> — Add multiple IG accounts at once.\n"
        "• <code>/stop &lt;username&gt;</code> — Stop monitoring a username.\n"
        "• <code>/active</code> — View all active monitoring tasks."
    )
    await message.answer(welcome_text)


@dp.message(Command("monitor"))
async def add_monitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/monitor &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        return await message.answer(f"⚠️ Already monitoring <b>@{username}</b>!")

    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": datetime.now(),
        "mode": "unban"
    }
    await message.answer(f"⚡ Super-fast monitoring active for <b>@{username}</b>!")


@dp.message(Command("banmonitor"))
async def add_banmonitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/banmonitor &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        return await message.answer(f"⚠️ Already monitoring <b>@{username}</b>!")

    monitored_accounts[key] = {
        "username": username,
        "chat_id": message.chat.id,
        "start_time": datetime.now(),
        "mode": "ban"
    }
    await message.answer(f"🚨 Ban monitoring active for <b>@{username}</b>!")


@dp.message(Command("bulk"))
async def add_bulk(message: types.Message):
    text = message.text.replace("/bulk", "").strip()
    if not text:
        return await message.answer("❌ Usage: <code>/bulk user1, user2, user3</code>")
    
    raw_list = text.replace(",", " ").split()
    added = []

    for u in raw_list:
        uname = u.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
        key = uname.lower()
        if uname and key not in monitored_accounts:
            monitored_accounts[key] = {
                "username": uname,
                "chat_id": message.chat.id,
                "start_time": datetime.now(),
                "mode": "unban"
            }
            added.append(f"@{uname}")

    if added:
        await message.answer(f"⚡ <b>{len(added)} accounts added for tracking!</b>\n" + ", ".join(added))
    else:
        await message.answer("⚠️ No new accounts were added.")


@dp.message(Command("stop"))
async def stop_monitor(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Usage: <code>/stop &lt;username&gt;</code>")
    
    raw = args[1]
    username = raw.split("instagram.com/")[-1].replace("/", "").replace("@", "").strip()
    key = username.lower()

    if key in monitored_accounts:
        del monitored_accounts[key]
        await message.answer(f"🛑 Stopped monitoring <b>@{username}</b>.")
    else:
        await message.answer(f"❌ Not currently monitoring <b>@{username}</b>.")


@dp.message(Command("active"))
async def show_active(message: types.Message):
    user_tasks = [v for k, v in monitored_accounts.items() if v["chat_id"] == message.chat.id]
    if not user_tasks:
        return await message.answer("ℹ️ No active monitoring tasks.")

    lines = ["📊 <b>Active Monitors:</b>\n"]
    for t in user_tasks:
        mode_icon = "✅ Unban" if t["mode"] == "unban" else "🚨 Ban"
        elapsed = datetime.now() - t["start_time"]
        h, r = divmod(int(elapsed.total_seconds()), 3600)
        m, s = divmod(r, 60)
        lines.append(f"• @{t['username']} ({mode_icon}) - Running for {h}h {m}m {s}s")

    await message.answer("\n".join(lines))


async def main():
    print("⚡ Truzd Monitor Bot is Live with Apify & Custom Cards!")
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
