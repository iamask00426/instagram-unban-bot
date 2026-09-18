import os
import asyncio
import aiohttp
import logging
import sys
import json
import re
import html
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
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

monitored_accounts = {}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BANNED_DP_PATH = os.path.join(BASE_DIR, "default_banned_dp.jpg")
FONT_PATH = os.path.join(BASE_DIR, "font.ttf")


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


def get_card_fonts():
    """Load bundled TrueType font for cross-platform Linux/Mac rendering"""
    if os.path.exists(FONT_PATH):
        try:
            return {
                "username": ImageFont.truetype(FONT_PATH, 48),
                "btn": ImageFont.truetype(FONT_PATH, 26),
                "stats_num": ImageFont.truetype(FONT_PATH, 34),
                "stats_lbl": ImageFont.truetype(FONT_PATH, 30),
                "name": ImageFont.truetype(FONT_PATH, 32),
            }
        except Exception as e:
            logging.error(f"Error loading bundled font.ttf: {e}")

    d = ImageFont.load_default()
    return {"username": d, "btn": d, "stats_num": d, "stats_lbl": d, "name": d}


async def create_profile_card(username, followers, posts, following, pic_url=None, is_banned=False):
    """Creates a 1000x550 Pure Black High-Res Profile Card with zero-overlap dynamic layout"""
    width, height = 1000, 550
    img = Image.new('RGB', (width, height), color='#000000')
    draw = ImageDraw.Draw(img)

    fonts = get_card_fonts()
    font_username = fonts["username"]
    font_btn = fonts["btn"]
    font_stats_num = fonts["stats_num"]
    font_stats_lbl = fonts["stats_lbl"]
    font_name = fonts["name"]

    av_center = (180, 275)
    av_r = 100
    av_size = (av_r * 2, av_r * 2)

    avatar_drawn = False

    # For BANNED account, use default_banned_dp.jpg
    if is_banned and os.path.exists(DEFAULT_BANNED_DP_PATH):
        try:
            dp_img = Image.open(DEFAULT_BANNED_DP_PATH).convert("RGB").resize(av_size)
            mask = Image.new('L', av_size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
            img.paste(dp_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
            avatar_drawn = True
        except Exception as e:
            logging.error(f"Error loading default banned DP: {e}")

    # If unbanned & has pic_url, download avatar
    if not avatar_drawn and pic_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(pic_url, timeout=5) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
                        av_img = Image.open(BytesIO(avatar_bytes)).convert("RGB").resize(av_size)
                        mask = Image.new('L', av_size, 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, av_size[0], av_size[1]), fill=255)
                        img.paste(av_img, (av_center[0]-av_r, av_center[1]-av_r), mask)
                        avatar_drawn = True
        except Exception as e:
            logging.error(f"Avatar download error: {e}")

    if not avatar_drawn:
        draw.ellipse([av_center[0]-av_r, av_center[1]-av_r, av_center[0]+av_r, av_center[1]+av_r], fill='#262626')
        first_char = username[0].upper() if username else 'U'
        draw.text((av_center[0]-15, av_center[1]-25), first_char, font=font_username, fill='#ffffff')

    # Username Title
    display_username = username if not is_banned else "UserNotFound"
    uname_x = 320
    uname_y = 200
    draw.text((uname_x, uname_y), display_username, font=font_username, fill='#ffffff')

    # DYNAMIC LAYOUT MATH FOR BUTTON & DOTS (ZERO OVERLAP!)
    try:
        uname_w = draw.textlength(display_username, font=font_username)
    except Exception:
        uname_w = len(display_username) * 28

    btn_left = int(uname_x + uname_w + 25)
    btn_top = 205
    btn_w = 130
    btn_h = 50
    draw.rounded_rectangle([btn_left, btn_top, btn_left+btn_w, btn_top+btn_h], radius=10, fill='#0095f6')
    draw.text((btn_left+26, btn_top+10), 'Follow', font=font_btn, fill='#ffffff')

    dot_x = btn_left + btn_w + 25
    dot_y = btn_top + 22
    for offset in [0, 14, 28]:
        draw.ellipse([dot_x+offset, dot_y, dot_x+offset+7, dot_y+7], fill='#ffffff')

    # Stats Line
    posts_str = str(posts)
    folls_str = str(followers)
    follg_str = str(following)
    st_y = 275
    st_lbl_y = 278

    draw.text((320, st_y), posts_str, font=font_stats_num, fill='#ffffff')
    pw = draw.textlength(posts_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(posts_str)*20
    draw.text((320 + pw + 10, st_lbl_y), 'posts', font=font_stats_lbl, fill='#a8a8a8')
    pw_lbl = draw.textlength('posts', font=font_stats_lbl) if hasattr(draw, 'textlength') else 75

    foll_x = int(320 + pw + 10 + pw_lbl + 35)
    draw.text((foll_x, st_y), folls_str, font=font_stats_num, fill='#ffffff')
    fw = draw.textlength(folls_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(folls_str)*20
    draw.text((foll_x + fw + 10, st_lbl_y), 'followers', font=font_stats_lbl, fill='#a8a8a8')
    fw_lbl = draw.textlength('followers', font=font_stats_lbl) if hasattr(draw, 'textlength') else 120

    follg_x = int(foll_x + fw + 10 + fw_lbl + 35)
    draw.text((follg_x, st_y), follg_str, font=font_stats_num, fill='#ffffff')
    fgw = draw.textlength(follg_str, font=font_stats_num) if hasattr(draw, 'textlength') else len(follg_str)*20
    draw.text((follg_x + fgw + 10, st_lbl_y), 'following', font=font_stats_lbl, fill='#a8a8a8')

    # Subtitle Name
    sub_title = username if not is_banned else "UserNotFound"
    draw.text((320, 330), sub_title, font=font_name, fill='#ffffff')

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
                    elapsed_str = f"{h} hours, {m} minutes, {s} seconds"

                    # UNBAN MODE: Trigger alert when account becomes LIVE
                    if mode == "unban" and uname in live_accounts:
                        ig = live_accounts[uname]
                        msg = (
                            f"Account Recovered | <a href='https://instagram.com/{raw_username}'>@{raw_username}</a> 🏆✅\n"
                            f"<i>Followers: {ig['followers']} | Following: {ig['following']}</i>\n"
                            f"⏱️ <i>Time taken: {elapsed_str}</i>"
                        )
                        try:
                            card_img = await create_profile_card(raw_username, ig['followers'], ig['posts'], ig['following'], pic_url=ig['pic_url'], is_banned=False)
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
                            f"<i>Status: UserNotFound</i>\n"
                            f"⏱️ <i>Time taken: {elapsed_str}</i>"
                        )
                        try:
                            card_img = await create_profile_card(raw_username, 0, 0, 0, is_banned=True)
                            await bot.send_photo(chat_id, photo=types.BufferedInputFile(card_img.read(), filename="card.png"), caption=msg)
                        except Exception as e:
                            logging.error(f"Error sending ban photo: {e}")
                            await bot.send_message(chat_id, text=msg, disable_web_page_preview=False)
                        
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
        lines.append(f"• @{t['username']} ({mode_icon}) - Running for {h}h {m}s")

    await message.answer("\n".join(lines))


async def main():
    print("⚡ Truzd Monitor Bot is Live with Bundled TTF Font & Dynamic Cards!")
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
