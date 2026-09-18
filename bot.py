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


async def create_profile_card(username, followers, posts, following, pic_url, status_title="✅ UNBANNED"):
    """Creates a high-resolution 800x420 modern dark profile card for Telegram alerts"""
    width, height = 800, 420
    img = Image.new('RGB', (width, height), color='#0f0e17')
    draw = ImageDraw.Draw(img)

    # Rounded outer card container
    draw.rounded_rectangle([15, 15, width-15, height-15], radius=24, fill='#161522', outline='#2a283e', width=2)

    # Top Right Status Badge
    badge_bg = '#1b3a2b' if "UNBANNED" in status_title or "ACTIVE" in status_title else '#451a1a'
    badge_border = '#22c55e' if "UNBANNED" in status_title or "ACTIVE" in status_title else '#ef4444'
    badge_text_color = '#4ade80' if "UNBANNED" in status_title or "ACTIVE" in status_title else '#f87171'

    draw.rounded_rectangle([width-200, 40, width-40, 85], radius=12, fill=badge_bg, outline=badge_border, width=1)
    draw.text((width-180, 52), status_title, fill=badge_text_color)

    # Avatar ring (Story Gradient simulation)
    avatar_center = (130, 210)
    avatar_radius = 75
    draw.ellipse([avatar_center[0]-avatar_radius-6, avatar_center[1]-avatar_radius-6, 
                   avatar_center[0]+avatar_radius+6, avatar_center[1]+avatar_radius+6], 
                  outline='#ec4899', width=4)

    # Download and draw circular avatar if pic_url provided
    avatar_drawn = False
    if pic_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(pic_url, timeout=5) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
                        av_img = Image.open(BytesIO(avatar_bytes)).convert("RGB").resize((150, 150))
                        mask = Image.new('L', (150, 150), 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, 150, 150), fill=255)
                        img.paste(av_img, (avatar_center[0]-avatar_radius, avatar_center[1]-avatar_radius), mask)
                        avatar_drawn = True
        except Exception as e:
            logging.error(f"Avatar download error: {e}")

    if not avatar_drawn:
        draw.ellipse([avatar_center[0]-avatar_radius, avatar_center[1]-avatar_radius, 
                       avatar_center[0]+avatar_radius, avatar_center[1]+avatar_radius], 
                      fill='#2e2b45')
        first_char = username[0].upper() if username else 'U'
        draw.text((avatar_center[0]-10, avatar_center[1]-10), first_char, fill='#ffffff')

    # Username & Title
    draw.text((240, 105), f"@{username}", fill='#ffffff')
    draw.text((240, 150), 'Instagram Account Monitoring Alert', fill='#a1a1aa')

    # Stats Grid Boxes (3 Pills: Posts, Followers, Following)
    box_y_top = 230
    box_y_bottom = 340

    # Posts Pill
    draw.rounded_rectangle([240, box_y_top, 390, box_y_bottom], radius=16, fill='#201e30', outline='#312e48', width=1)
    draw.text((285, box_y_top + 25), str(posts), fill='#ffffff')
    draw.text((275, box_y_top + 65), 'POSTS', fill='#94a3b8')

    # Followers Pill (Highlighted)
    draw.rounded_rectangle([410, box_y_top, 570, box_y_bottom], radius=16, fill='#201e30', outline='#3b82f6', width=2)
    draw.text((450, box_y_top + 25), str(followers), fill='#38bdf8')
    draw.text((440, box_y_top + 65), 'FOLLOWERS', fill='#94a3b8')

    # Following Pill
    draw.rounded_rectangle([590, box_y_top, 740, box_y_bottom], radius=16, fill='#201e30', outline='#312e48', width=1)
    draw.text((645, box_y_top + 25), str(following), fill='#ffffff')
    draw.text((625, box_y_top + 65), 'FOLLOWING', fill='#94a3b8')

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
            async with session.post(API_URL, json={"usernames": usernames_list}, timeout=35) as response:
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
            for i in range(0, len(usernames), 30):
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
                            card_img = await create_profile_card(raw_username, ig['followers'], ig['posts'], ig['following'], ig['pic_url'], status_title="✅ UNBANNED")
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
    print("⚡ Truzd Monitor Bot is Live with High-Res Profile Cards!")
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
